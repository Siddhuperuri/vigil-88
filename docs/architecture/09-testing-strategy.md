# 09 — Testing Strategy

> Status: DESIGN. No tests exist. The infrastructure in §4 and §5 is built in P0, before
> any engine, because it is what makes the engines testable at all.

## 1. The central problem

A CV system is usually tested by running it on video and looking at the output. That is not
testing; it is demonstration. It is slow, needs a GPU, needs footage, and — fatally — it is
**not reproducible**, because real video replayed through a live-oriented pipeline drops
different frames each run.

So the strategy rests on one decision: **the pipeline's decision logic must be fully
testable with no GPU, no model weights, and no video files.** Everything else follows.

Three mechanisms make it true:

1. **`MockDetector`** — replays scripted detections from YAML. Removes the model.
2. **Synthetic scene generator** — produces frames and ground truth programmatically.
   Removes the footage.
3. **`ManualClock`** — time advances only when a test advances it. Removes wall-clock
   flakiness and makes a 10-minute temporal scenario run in 2 ms.

With those, "does a flickering detection confirm an incident?" becomes a 30-line unit test
that runs in milliseconds and gives the same answer on every machine. Without them, it is a
manual check nobody repeats.

## 2. Test tiers

| Tier | Location | Count (target) | Runtime | Needs GPU | Runs |
|------|----------|---------------|---------|-----------|------|
| Unit | `tests/unit/` mirroring `src/vigil/` | hundreds | <10 s total | No | Every save |
| Contract | `tests/contract/` | ~1 per protocol implementation | <5 s | No | Every save |
| Property | within unit, `hypothesis` | ~20 | <20 s | No | Every save |
| Integration | `tests/integration/` | ~20 | <60 s | No | Pre-commit |
| Smoke | `tests/integration/test_startup.py` | ~5 | <30 s | No | Pre-commit |
| Performance | `tests/perf/`, marked `slow` | ~10 | minutes | **Yes** | Explicitly, at phase boundaries |

The default `pytest` run excludes `slow`. The full gate (01 §12) is the pre-commit
contract. Performance tests are a separate, deliberate act with their own output
(`docs/PERFORMANCE.md`) because a benchmark that runs in CI on shared hardware produces
numbers that mean nothing.

## 3. What gets tested hardest

Coverage is targeted where a bug causes a wrong alarm, not spread uniformly:

| Package | Coverage floor | Why |
|---------|---------------|-----|
| `temporal/` | 95% | The confidence mechanism. A bug here means every incident's confidence is wrong |
| `verification/` | 95% | Decides what reaches the operator |
| `severity/` | 90% | Decides what the operator looks at first |
| `events/` | 90% | Module logic |
| `domain/` | 90% | Cheap to test; everything depends on it |
| `scene/` | 85% | Geometry is where off-by-one errors hide |
| `tracking/` | 85% | Association logic |
| `ingest/`, `vision/`, `api/`, `cli/` | no floor | Dominated by I/O and third-party calls; integration-tested instead. Chasing a coverage number here produces mock-heavy tests that verify the mocks |

Deliberately no global coverage number. A single figure lets 100% coverage of trivial glue
hide 40% coverage of the accumulator.

## 4. Synthetic scene generator (`tests/synthetic/`)

Generates video and ground truth from a declarative script. This is the workhorse.

```python
scene = SyntheticScene(width=1280, height=720, fps=25, clock=ManualClock())
scene.add_zone("yard", polygon=[(0.5, 0.4), (0.95, 0.4), (0.95, 0.95), (0.5, 0.95)],
               kind=ZoneKind.RESTRICTED, criticality=3)
scene.add_actor(
    Actor(object_class=ObjectClass.PERSON, size_px=(60, 160))
      .at(t_ms=0,     pos=(0.10, 0.60))
      .walk_to(t_ms=4000, pos=(0.70, 0.60))     # enters "yard" around t=2600
      .dwell(t_ms=6000)
      .walk_to(t_ms=9000, pos=(0.10, 0.60)),
)
clip = scene.render()        # frames + per-frame ground truth
```

It produces three things:

1. **Frames** — flat-shaded rectangles on a textured background. Not photorealistic, and it
   does not need to be: it is testing the *logic* downstream of detection, and the detector
   is mocked anyway. It is realistic enough to exercise real decode, resize, and letterbox
   paths when needed.
2. **Ground truth** — exact boxes, classes, and identities per frame. So tracker ID switches
   and scene-geometry errors are measurable, not eyeballed.
3. **A detection script** — ground truth optionally perturbed, which is the interesting
   part.

### Perturbations — the point of the whole thing

```python
clip.perturb(
    DropFrames(probability=0.1),               # detector misses
    JitterBoxes(sigma_px=4),                   # localisation noise
    ConfidenceNoise(sigma=0.08),
    FlickerClass(probability=0.05),            # person <-> unknown
    IdSwitch(at_ms=5200, between=(1, 2)),      # tracker failure
    GhostDetections(rate_per_frame=0.2),       # false positives
    FrameGap(at_ms=7000, duration_ms=2500),    # network blip -> epoch bump
    BrightnessStep(at_ms=3000, delta=0.4),     # lights on -> scene instability
)
```

Each perturbation corresponds to a real-world failure the system claims to suppress, which
makes the claims testable:

| Perturbation | Asserts |
|--------------|---------|
| `DropFrames` | `min_support_ratio` tolerates gaps without either missing a real event or confirming on flicker |
| `GhostDetections` | Isolated false positives never reach promotion |
| `IdSwitch` | `MotionSanityValidator` catches the implied teleport |
| `FrameGap` | Epoch bump resets tracks; `zone:` accumulators decay rather than vanish (07 §5) |
| `BrightnessStep` | `SceneStabilityValidator` blacks out the window |
| `FlickerClass` | A class flicker does not create a second accumulator that confirms separately |

This is how "false-positive suppression" stops being a claim in a document and becomes a
row in a test report.

## 5. MockDetector and fixtures

```yaml
# tests/fixtures/detections/intrusion_basic.yaml
meta: { camera_id: test-cam, width: 1280, height: 720, fps: 25 }
frames:
  - { index: 0,  detections: [] }
  - { index: 10, detections: [{ class: person, bbox: [100, 300, 160, 460], conf: 0.88 }] }
  - { index: 11, detections: [{ class: person, bbox: [112, 300, 172, 460], conf: 0.91 }] }
  # ...
  - { index: 65, repeat_until: 140, detections:
        [{ class: person, bbox: [700, 320, 760, 480], conf: 0.90, drift: [1.2, 0.0] }] }
```

`repeat_until` with `drift` keeps a 10-second scenario to a dozen lines instead of 250
frames of YAML. `MockDetector` is deterministic, needs no GPU, and runs at thousands of
frames per second, so a full temporal scenario is a unit test rather than an integration
test.

Fixtures are authored by hand for designed cases, and generated from synthetic clips for
bulk cases. When a real false positive is found in the field, the reproducing fixture is
committed with the fix — the regression suite grows from real failures, which is the only
way a suppression suite earns trust.

## 6. Property tests (`hypothesis`)

For invariants that must hold over *all* inputs, where examples are not enough. These are
the specific properties from [04-ai-pipeline.md](04-ai-pipeline.md) §9:

```python
@given(signals=lists(floats(0.0, 1.0), min_size=1, max_size=200),
       gaps_ms=lists(integers(1, 5000), min_size=1, max_size=200))
def test_log_odds_stays_bounded(signals, gaps_ms):
    acc = Accumulator(params=DEFAULTS, clock=ManualClock())
    for s, gap in zip(signals, gaps_ms):
        clock.advance_ms(gap)
        state = acc.observe(obs(signal=s))
        assert -DEFAULTS.l_bound <= state.log_odds <= DEFAULTS.l_bound

@given(signals=lists(floats(0.0, 1.0), min_size=5, max_size=100))
def test_stronger_evidence_never_lowers_confidence(signals):
    weaker = [min(s, 0.5) for s in signals]
    assert final_log_odds(signals) >= final_log_odds(weaker)

@given(n=integers(1, 500))
def test_neutral_signal_never_accumulates(n):
    acc = Accumulator(params=DEFAULTS, clock=ManualClock())
    for _ in range(n):
        clock.advance_ms(100)
        acc.observe(obs(signal=0.5))
    assert abs(acc.state.log_odds) < 1e-9
```

Also property-tested: polygon containment (a point inside a polygon stays inside under
uniform scaling); bbox IoU (symmetry, identity, range); zone normalization round-trips
within half a pixel; ULID monotonicity; and tracker ID stability (an actor on a continuous
path keeps one ID under bounded jitter).

The third test above is the one worth the most: it mechanically proves that a module which
is persistently unsure can never confirm an incident by being unsure for long enough.

## 7. Contract tests (`tests/contract/`)

Parameterised across every registered implementation, so adding an implementation
automatically subjects it to the suite:

```python
@pytest.mark.parametrize("module", all_registered_modules(), ids=module_id)
class TestDetectionModuleContract:
    def test_observe_is_pure(self, module):           # same scene twice -> same observations
    def test_observe_never_raises_on_empty_scene(self, module):
    def test_observe_respects_time_budget(self, module):
    def test_signals_are_in_unit_range(self, module):
    def test_declares_capabilities_it_actually_uses(self, module):
    def test_resets_state_on_epoch_change(self, module):
    def test_params_model_rejects_unknown_keys(self, module):
    def test_has_readme_documenting_false_positives(self, module):
```

`test_declares_capabilities_it_actually_uses` runs the module against a `SceneState` with
each declared capability removed in turn and asserts behaviour changes. It catches both an
over-declared capability (making a module needlessly unavailable) and an under-declared one
(making it silently wrong) — the two ways capability negotiation (D-008) degrades into
theatre.

`test_has_readme_documenting_false_positives` is a real test: a module without a documented
failure mode has not been thought about carefully enough to trust.

Equivalent contract suites exist for `Detector` (coordinates in source space, batch
equivalence, deterministic given fixed input), `FrameSource` (lifecycle, `None` at EOS,
`SourceError` on failure), `EventValidator` (verdict always has a reason; never promotes),
and `AlertChannel` (`is_available()` honest; `send()` never raises).

## 8. Integration tests

Full pipeline, `MockDetector`, `ManualClock`, synthetic scenes, in-memory SQLite, no GPU.

```python
def test_single_intrusion_produces_exactly_one_incident(pipeline, clip):
    pipeline.run_clip(clip)                       # 12 s of synthetic video
    incidents = pipeline.incidents()
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc.event_type is EventType.INTRUSION
    assert inc.severity is Severity.HIGH
    assert 500 <= detection_latency_ms(inc) <= 2000
    assert inc.status is IncidentStatus.RESOLVED
    assert {e.kind for e in inc.evidence} >= {EvidenceKind.SNAPSHOT_ANNOTATED,
                                              EvidenceKind.EXPLANATION}
```

`assert len(incidents) == 1` is the assertion the whole architecture exists to make
possible. A per-frame classifier would produce 200 for this clip.

Scenarios in the suite:

| Scenario | Asserts |
|----------|---------|
| Clean single intrusion | Exactly one incident, correct severity, bounded detection latency |
| Two simultaneous people in one zone | Two incidents (separate `track:` subjects), not one merged or four |
| Person walks the zone boundary for 30 s | At most one confirm and one resolve — hysteresis holds |
| Ghost detections only | Zero incidents |
| Frame gap mid-incident | Incident stays open, annotated `stream_interrupted`, epoch bumped |
| Brightness step | Zero incidents during the blackout window |
| Operator dismisses | Terminal `DISMISSED`, counted in the FP rate, no re-fire during cooldown |
| Module raises every call | Module quarantined, pipeline healthy, other modules unaffected |
| Zone file invalid | Camera starts, module UNAVAILABLE with the parse error as its reason |
| Replay twice | **Byte-identical** incident records bar ULIDs and wall timestamps |

The last one is the reproducibility gate. If two runs of the same clip disagree, something
reads a clock or a global it should not, and every other test result becomes suspect.

## 9. Determinism

Non-negotiable, because a flaky test in this suite will be ignored rather than fixed, and
then the suite is worthless.

| Source of nondeterminism | Control |
|--------------------------|---------|
| Wall clock | `ManualClock` everywhere; grep gate on `time.time(`/`datetime.now(` (01 §12) |
| RNG | `core/rng.py`, seeded per test by fixture |
| Thread interleaving | Integration tests run the pipeline in **single-threaded mode** (`pipeline.sync=true`), which executes stages inline. Concurrency is tested separately and explicitly |
| Dict/set ordering | Domain collections are tuples and `frozenset`s; anything order-sensitive sorts explicitly |
| Float arithmetic | Comparisons use `pytest.approx` with stated tolerance; never `==` |
| Filesystem | `tmp_path` fixture; `paths.data_dir` redirected per test |
| GPU nondeterminism | Not present in the default suite (no GPU). Perf tests report variance rather than asserting exact values |

`pipeline.sync=true` is a first-class config mode, not a test hack: the same stage graph
runs inline instead of across workers. It is also what `vigil replay` uses, so the
deterministic path is exercised by a real command and cannot rot.

## 10. Performance testing

Separate in kind, not just in speed. Owned by `vigil bench`, which writes
`docs/PERFORMANCE.md` — the only thing permitted to make a performance claim (README rule 2).

```powershell
vigil bench --cameras 1,2,3,4 --backend ultralytics --weights yolov8n.pt `
            --input-size 640,512 --precision fp16,fp32 --duration 120
```

Reports per configuration, with p50/p95/p99 and standard deviation across runs:
inference latency, end-to-end latency (capture to incident), achieved pipeline FPS, frames
skipped and dropped, GPU utilisation and VRAM peak, CPU and RSS, queue depth distribution,
and thermal clock behaviour over the run.

The last one matters specifically on this machine: on a **mobile** RTX 3050, sustained load
causes clock reduction, so a 10-second benchmark overstates sustained throughput. Minimum
benchmark duration is 120 s and the report includes the first-30 s versus last-30 s delta.
A benchmark that does not surface thermal throttling on a laptop GPU is measuring the wrong
thing.

`docs/PERFORMANCE.md` records the commit, config hash, driver version, and date. It is
regenerated at every phase boundary, never hand-edited, and performance regressions are
reviewed by diffing it.

## 11. What is not tested, and why

Stated honestly rather than left as a gap to be discovered:

| Not tested | Why | Mitigation |
|------------|-----|------------|
| Detector accuracy (mAP) | We consume models, we do not train them. Measuring mAP needs a labelled dataset we do not have and would not own | Record the published metrics and licence of each weight file in `var/models/MANIFEST.json`; evaluate candidates in P1 on *our* footage against measured latency |
| Real-world false-positive rate | Cannot be known from synthetic data. This is the honest limitation of the whole strategy | Dismissal rate per module version is measured in production and surfaced in Analytics (06 §9). That is the real number, and it only exists once the system runs on real cameras |
| Fire/smoke detection quality | No dataset in P0–P5 | Blocks P6; stated in `docs/LIMITATIONS.md` |
| Real RTSP camera behaviour | No IP camera on this machine | Tests use a local RTSP server fed from a file in P1; a real camera is a manual acceptance step with a written checklist in `docs/OPERATIONS.md` |
| Long-run stability (days) | Too slow for CI | A nightly soak run with synthetic sources at P4, asserting bounded RSS, bounded handle count, and no unbounded growth in any registry |
| Browser compatibility | Single known target | Chromium-based browsers only, stated in the README |

The second row is the one to be clear-eyed about: **no amount of synthetic testing tells you
the real false-positive rate.** Synthetic tests prove the suppression *mechanisms* work as
specified. Whether the specified thresholds are right for a particular camera is an
empirical question answerable only with that camera's footage, which is why the dismissal
rate is instrumented from P4 and why rejected candidates are persisted (05 §5).

## 12. Fixtures (`tests/conftest.py`)

```python
@pytest.fixture
def clock() -> ManualClock: ...                    # starts at a fixed epoch

@pytest.fixture
def settings(tmp_path, clock) -> Settings: ...     # data_dir -> tmp_path, sync=True

@pytest.fixture
def pipeline(settings, clock) -> SyncPipeline: ... # full graph, MockDetector, in-memory DB

@pytest.fixture
def scene_builder(clock) -> SceneStateBuilder: ... # hand-build a SceneState in 3 lines
```

`SceneStateBuilder` deserves emphasis: module unit tests must be able to construct a scene
directly, without a pipeline, a detector, or frames.

```python
scene = scene_builder.with_zone("yard", RESTRICTED, criticality=3) \
                     .with_track(1, PERSON, at=(0.7, 0.6), hits=5, in_zones=["yard"]) \
                     .build()
observations = IntrusionModule().observe(scene)
assert len(observations) == 1 and observations[0].signal == pytest.approx(0.70, abs=0.01)
```

If writing a module's unit test requires more setup than that, the module contract is
leaking and the contract is wrong — not the test.
