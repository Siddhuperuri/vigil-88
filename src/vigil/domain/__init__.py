"""L2: frozen, slotted value objects. No I/O, no third-party imports (02 §5)."""

from vigil.domain.alert import Alert, AlertDelivery
from vigil.domain.camera import Camera, CameraHealth, CameraStateChanged, SourceSpec
from vigil.domain.detection import Detection, DetectionSet, ModelDescriptor, PreparedFrame
from vigil.domain.enums import (
    CameraState,
    EventType,
    EvidenceKind,
    IncidentStatus,
    ObjectClass,
    SceneFact,
    Severity,
    SourceKind,
    SpatialRelationKind,
    TrackFlag,
    ValidatorOutcome,
    ZoneKind,
)
from vigil.domain.event import AccumulatorState, CandidateEvent, ValidatorVerdict, VerifiedEvent
from vigil.domain.evidence import Evidence
from vigil.domain.frame import Frame, FrameMeta
from vigil.domain.incident import (
    AccumulatorSample,
    Incident,
    IncidentExplanation,
    IncidentTransition,
    ObservationSummary,
    SeverityAssessment,
    SeverityFactor,
    SystemSnapshot,
)
from vigil.domain.metric import StageLatency, SystemMetric
from vigil.domain.observation import Observation, SubjectKey
from vigil.domain.scene import (
    SceneState,
    Schedule,
    SpatialRelation,
    Zone,
    ZoneOccupancy,
)
from vigil.domain.track import TrackedObject, TrackPoint, Trajectory

__all__ = [
    "AccumulatorSample",
    "AccumulatorState",
    "Alert",
    "AlertDelivery",
    "Camera",
    "CameraHealth",
    "CameraState",
    "CameraStateChanged",
    "CandidateEvent",
    "Detection",
    "DetectionSet",
    "EventType",
    "Evidence",
    "EvidenceKind",
    "Frame",
    "FrameMeta",
    "Incident",
    "IncidentExplanation",
    "IncidentStatus",
    "IncidentTransition",
    "ModelDescriptor",
    "ObjectClass",
    "Observation",
    "ObservationSummary",
    "PreparedFrame",
    "SceneFact",
    "SceneState",
    "Schedule",
    "Severity",
    "SeverityAssessment",
    "SeverityFactor",
    "SourceKind",
    "SourceSpec",
    "SpatialRelation",
    "SpatialRelationKind",
    "StageLatency",
    "SubjectKey",
    "SystemMetric",
    "SystemSnapshot",
    "TrackFlag",
    "TrackPoint",
    "TrackedObject",
    "Trajectory",
    "ValidatorOutcome",
    "ValidatorVerdict",
    "VerifiedEvent",
    "Zone",
    "ZoneKind",
    "ZoneOccupancy",
]
