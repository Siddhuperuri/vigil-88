"""The verification harnesses are measuring instruments; their arithmetic and wording are tested.

What these tests do not do is run the instruments against the hardware they exist for. That needs
a real camera, a real card and hours of time, and is recorded (or not) in docs/verification/.
"""

from __future__ import annotations

import numpy as np
import pytest
from scripts.verify import gpu_check, proc_sampler, rtsp_check

# ------------------------------------------------------------------ the process-tree sampler


@pytest.mark.parametrize(
    ("name", "command", "role"),
    [
        ("VIGIL-88.exe", ["VIGIL-88.exe"], "shell"),
        ("VIGIL-88.exe", ["VIGIL-88.exe", "--type=renderer"], "renderer"),
        ("VIGIL-88.exe", ["VIGIL-88.exe", "--type=gpu-process"], "gpu"),
        ("VIGIL-88.exe", ["VIGIL-88.exe", "--type=utility"], "utility"),
        ("vigil-engine.exe", ["vigil-engine.exe", "serve"], "engine"),
        ("python.exe", ["python.exe", "-m", "vigil", "serve", "--port", "0"], "engine"),
        ("electron.exe", ["electron.exe", "."], "shell"),
        ("conhost.exe", ["conhost.exe"], "other"),
    ],
)
def test_a_process_is_given_its_role(name: str, command: list[str], role: str) -> None:
    assert proc_sampler.role_of(name, command) == role


def rows(total: list[float], handles: float = 500.0) -> list[dict[str, float]]:
    return [
        {
            "t_s": 120 + i * 15,
            "processes": 6.0,
            "rss_total_mb": mb,
            "handles_total": handles,
            "threads_total": 120.0,
            **{f"rss_{r}_mb": mb / 4 for r in proc_sampler.ROLES},
        }
        for i, mb in enumerate(total)
    ]


def test_a_flat_run_is_bounded_and_says_how_short_it_was() -> None:
    lines, bounded = proc_sampler.verdict(rows([800.0] * 40), 15)
    assert bounded and "A short run does not show" in lines[1]
    assert "of the 480 the plan asks for" in lines[0]
    assert lines[-1] == "Verdict on this run: memory and handles stayed bounded."


def test_a_leaking_run_is_not_called_bounded() -> None:
    lines, bounded = proc_sampler.verdict(rows([800.0 + 20 * i for i in range(40)]), 15)
    assert not bounded and lines[-1].startswith("Verdict on this run: NOT shown")


def test_growing_handles_alone_fail_the_verdict() -> None:
    growing = rows([800.0] * 40)
    for i, row in enumerate(growing):
        row["handles_total"] = 500.0 + 30 * i
    assert not proc_sampler.verdict(growing, 15)[1]


def test_too_few_samples_is_said_instead_of_judged() -> None:
    lines, bounded = proc_sampler.verdict(rows([800.0] * 3), 1)
    assert not bounded and "Too short to judge" in lines[-1]


# ------------------------------------------------------------------ the RTSP check


def test_the_address_is_recorded_without_its_credentials() -> None:
    shown = rtsp_check.address_without_credentials("rtsp://someone:hidden@10.0.0.9:8554/live/1")
    assert shown == "rtsp://10.0.0.9:8554/live/1" and "hidden" not in shown


def test_no_frame_is_not_called_a_pass() -> None:
    assert rtsp_check.summarise(0, 5.0, [], None)["verdict"].startswith("NO FRAME")


def test_frames_without_a_break_say_reconnection_was_not_tried() -> None:
    out = rtsp_check.summarise(300, 30.0, [], 0.4)
    assert out["measured_fps"] == 10.0 and "NOT exercised" in str(out["verdict"])


def test_a_recovered_outage_reports_how_long_it_took() -> None:
    outage = {"started_s": 20.0, "recovered": True, "seconds_to_recover": 4.5}
    assert "RECOVERED 1 outage(s); slowest took 4.5 s" in str(
        rtsp_check.summarise(500, 60.0, [outage], 0.4)["verdict"]
    )


def test_an_outage_that_never_recovered_is_a_failure() -> None:
    outage = {"started_s": 20.0, "recovered": False}
    assert str(rtsp_check.summarise(100, 60.0, [outage], 0.4)["verdict"]).startswith(
        "FAILED TO RECOVER"
    )


def test_asking_for_help_prints_usage() -> None:
    assert rtsp_check.main([]) == 2


# ------------------------------------------------------------------ the GPU check


def test_the_comparison_is_the_largest_difference() -> None:
    a = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    assert gpu_check.compare(a, a + np.array([0.0, 0.5, 0.1], dtype=np.float32)) == pytest.approx(
        0.5
    )


def test_a_machine_with_no_card_says_there_is_no_gpu_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_check, "smi", lambda: None)
    env = gpu_check.environment()
    assert env["card"] is None
    assert "no NVIDIA driver" in env["verdict"] or env["cuda_provider_offered"] is False


def test_a_dry_run_does_no_gpu_work_and_fails_without_cuda(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        gpu_check,
        "environment",
        lambda: {
            "card": None,
            "providers": ["CPUExecutionProvider"],
            "cuda_provider_offered": False,
        },
    )
    assert gpu_check.main(["--dry-run"]) == 1
    assert "CPUExecutionProvider" in capsys.readouterr().out


def test_smi_reads_a_card_row(monkeypatch: pytest.MonkeyPatch) -> None:
    class Done:
        stdout = "NVIDIA GeForce RTX, 555.12, 8192, 900, 41\n"

    monkeypatch.setattr(gpu_check.shutil, "which", lambda _name: "nvidia-smi")
    monkeypatch.setattr(gpu_check.subprocess, "run", lambda *_a, **_k: Done())
    assert gpu_check.smi() == {
        "name": "NVIDIA GeForce RTX",
        "driver": "555.12",
        "memory_total_mb": "8192",
        "memory_used_mb": "900",
        "temperature_c": "41",
    }


def test_smi_is_none_without_a_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_check.shutil, "which", lambda _name: None)
    assert gpu_check.smi() is None
