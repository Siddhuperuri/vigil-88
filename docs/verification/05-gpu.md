# 05. A real NVIDIA GPU

**Claim:** with the GPU build, detection really runs on the card, and gives the same answers as the CPU.

## Why a check is needed

onnxruntime falls back to the CPU **silently** when CUDA cannot load (a missing library, a driver that is
too old). A run that "worked" can have used the CPU the whole time. So the check asks which provider the
session actually uses.

## Needs

- A machine with an NVIDIA card and driver, and the GPU build of the engine environment
  (`uv sync --extra onnx-gpu`). The installer carries the CPU engine only (plan decision 2), so this is a
  check of the **source tree** build.
- A model file: `vigil models fetch yolox_s`.

## Run

Read the environment first. This does no GPU work:

```powershell
uv run python scripts/verify/gpu_check.py --dry-run
```

It reports the card and driver from `nvidia-smi`, the onnxruntime version, and which providers are offered.
Then the real check, on a machine where you are content to use the card for a minute:

```powershell
uv run python scripts/verify/gpu_check.py models/yolox_s.onnx --runs 50 --out gpu-evidence.json
```

It opens the model on CUDA and on the CPU, and reports the provider used, median and p95 latency on each,
the largest difference between the two outputs on one input, and the card's memory before and after.
It passes only if the provider is CUDA **and** the outputs agree within 0.01.

## Also by hand

1. `uv run vigil doctor` lists *detection* as available and names the device.
2. Run two cameras for ten minutes. *System* shows GPU use, memory and temperature, never a bare zero.
3. Pull the driver's attention (start a game, or a second inference) and watch the degradation banner:
   the engine should step down and say why, and step back up when the load ends.
4. A driver reset (`pnputil /restart-device` on the card, as an administrator) while running is the
   GPU-driver drill. **Not scripted**, and not yet drilled: the expected behaviour is a crashed or failed
   inference worker that the engine reports, not a hang.

## Status

The environment can be read with `--dry-run` (see [evidence](evidence/)). **The benchmark has not been run**
as part of this work; a GPU run was declined earlier and was not repeated. Run it when convenient.

Record the result with [the template](evidence/TEMPLATE.md).
